import json
import os
import uuid
import requests

_K8S_API = "https://kubernetes.default.svc"
_SA_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_SA_CA = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
_NAMESPACE = "nuvolaris"


def _k8s_headers():
    with open(_SA_TOKEN) as f:
        token = f.read().strip()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def main(args):
    job_name = args.get("job_name") or f"sparkjob-{uuid.uuid4().hex[:8]}"
    main_class = args.get("main_class", "org.apache.spark.examples.SparkPi")
    main_file = args.get(
        "main_application_file",
        "local:///opt/spark/examples/jars/spark-examples_2.12-3.5.0.jar",
    )
    raw_args = args.get("arguments", ["10"])
    arguments = raw_args if isinstance(raw_args, list) else [str(raw_args)]
    instances = int(args.get("instances", 2))

    sparkjob = {
        "apiVersion": "nuvolaris.org/v1",
        "kind": "SparkJob",
        "metadata": {"name": job_name, "namespace": _NAMESPACE},
        "spec": {
            "application": {
                "mainClass": main_class,
                "mainApplicationFile": main_file,
                "arguments": arguments,
                "source": {"type": "url", "url": main_file},
            },
            "spark": {
                "master": "spark://spark-master:7077",
                "conf": {},
                "driver": {"cores": 1, "memory": "512Mi", "serviceAccount": "spark"},
                "executor": {"instances": instances, "cores": 1, "memory": "512Mi"},
            },
            "execution": {
                "restartPolicy": "Never",
                "timeout": 300,
                "backoffLimit": 0,
            },
            "monitoring": {
                "enabled": False,
                "eventLog": False,
                "historyServer": False,
            },
        },
    }

    url = (
        f"{_K8S_API}/apis/nuvolaris.org/v1"
        f"/namespaces/{_NAMESPACE}/sparkjobs"
    )

    try:
        resp = requests.post(
            url,
            json=sparkjob,
            headers=_k8s_headers(),
            verify=_SA_CA if os.path.exists(_SA_CA) else False,
            timeout=10,
        )
    except Exception as exc:
        return {"error": True, "message": str(exc)}

    if resp.status_code in (200, 201):
        return {
            "job_id": job_name,
            "status": "submitted",
            "message": f"SparkJob {job_name} created — watcher will track driver pod",
        }
    if resp.status_code == 409:
        return {
            "job_id": job_name,
            "status": "already_exists",
            "message": f"SparkJob {job_name} already exists",
        }

    body = {}
    try:
        body = resp.json()
    except Exception:
        pass
    return {
        "error": True,
        "http_status": resp.status_code,
        "message": body.get("message", resp.text),
    }
