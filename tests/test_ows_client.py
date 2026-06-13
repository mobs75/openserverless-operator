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
from unittest.mock import MagicMock, patch
import requests

from nuvolaris.spark_notifier import OWSClient, DeliveryError


def _make_client(url="http://fake-ows/trigger", token="tok"):
    client = OWSClient.__new__(OWSClient)
    client._trigger_url = url
    client._auth_token = token
    return client


def _resp(status_code: int):
    r = MagicMock()
    r.status_code = status_code
    return r


class TestOWSClientSuccess:
    def test_post_200_no_retry(self):
        """R3.AC1: successful POST → no retry, function returns normally."""
        client = _make_client()
        with patch("nuvolaris.spark_notifier.requests.post", return_value=_resp(200)) as mock_post:
            client.post_event("job-1", "Succeeded", "nuvolaris", "2026-01-01T00:00:00Z")
            assert mock_post.call_count == 1

    def test_payload_contains_required_fields(self):
        """R3.AC1: payload must contain job_id, status, namespace, timestamp."""
        client = _make_client(token="mytoken")
        with patch("nuvolaris.spark_notifier.requests.post", return_value=_resp(200)) as mock_post:
            client.post_event("job-2", "Failed", "ns-a", "2026-06-13T10:00:00Z")
            _, kwargs = mock_post.call_args
            payload = kwargs["json"]
            assert payload["job_id"] == "job-2"
            assert payload["status"] == "Failed"
            assert payload["namespace"] == "ns-a"
            assert payload["timestamp"] == "2026-06-13T10:00:00Z"

    def test_bearer_auth_header_sent(self):
        """R3.AC4: credentials from Secret env → Bearer token in Authorization header."""
        client = _make_client(token="secret-token")
        with patch("nuvolaris.spark_notifier.requests.post", return_value=_resp(202)) as mock_post:
            client.post_event("job-3", "Succeeded", "nuvolaris", "2026-01-01T00:00:00Z")
            _, kwargs = mock_post.call_args
            assert kwargs["headers"]["Authorization"] == "Bearer secret-token"


class TestOWSClientRetry:
    def test_retries_on_503_then_raises(self):
        """R3.AC2: 5xx → retry 3x, then DeliveryError (R3.AC3)."""
        client = _make_client()
        with patch("nuvolaris.spark_notifier.requests.post", return_value=_resp(503)) as mock_post, \
             patch("nuvolaris.spark_notifier.time.sleep"):
            with pytest.raises(DeliveryError):
                client.post_event("job-4", "Failed", "nuvolaris", "2026-01-01T00:00:00Z")
            assert mock_post.call_count == 3

    def test_retries_on_connection_error_then_raises(self):
        """R3.AC2: ConnectionError → retry 3x, then DeliveryError."""
        client = _make_client()
        with patch("nuvolaris.spark_notifier.requests.post",
                   side_effect=requests.ConnectionError("refused")) as mock_post, \
             patch("nuvolaris.spark_notifier.time.sleep"):
            with pytest.raises(DeliveryError):
                client.post_event("job-5", "Succeeded", "nuvolaris", "2026-01-01T00:00:00Z")
            assert mock_post.call_count == 3

    def test_succeeds_on_second_attempt(self):
        """One 503 then one 200 → exactly 2 attempts, no exception."""
        client = _make_client()
        responses = [_resp(503), _resp(200)]
        with patch("nuvolaris.spark_notifier.requests.post",
                   side_effect=responses) as mock_post, \
             patch("nuvolaris.spark_notifier.time.sleep"):
            client.post_event("job-6", "Succeeded", "nuvolaris", "2026-01-01T00:00:00Z")
            assert mock_post.call_count == 2

    def test_does_not_retry_on_4xx(self):
        """4xx errors are not server errors → no retry, function returns normally."""
        client = _make_client()
        with patch("nuvolaris.spark_notifier.requests.post", return_value=_resp(400)) as mock_post:
            client.post_event("job-7", "Succeeded", "nuvolaris", "2026-01-01T00:00:00Z")
            assert mock_post.call_count == 1
