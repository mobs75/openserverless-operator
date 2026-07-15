import json
import datetime


def main(args):
    job_id = args.get("job_id", "unknown")
    status = args.get("status", "unknown")
    namespace = args.get("namespace", "nuvolaris")
    event_ts = args.get("timestamp", "")
    received_at = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

    event = {
        "event_type": "spark_job_terminal",
        "job_id": job_id,
        "status": status,
        "namespace": namespace,
        "event_timestamp": event_ts,
        "received_at": received_at,
    }

    print(json.dumps(event))

    return {
        "processed": True,
        "job_id": job_id,
        "status": status,
        "received_at": received_at,
    }
