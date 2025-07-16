# CouchDB Operator for OpenServerless

This repository contains a Kubernetes operator built with [Kopf](https://kopf.readthedocs.io/en/stable/) that automatically manages the installation and configuration of CouchDB in the OpenServerless platform.

## Prerequisites

- Kubernetes >= 1.21 (e.g., MicroK8s, Kind, or compatible cluster)
- Python 3.11+
- Docker
- [Task](https://taskfile.dev/#/installation) (task runner)

## Initial Setup

1. Clone the repository and move into the operator directory:

   ```bash
   git clone https://github.com/mobs75/openserverless-operator.git
   cd openserverless-operator/couchdb-operator
   ```

2. Make sure the `nuvolaris` namespace exists:

   ```bash
   kubectl create namespace nuvolaris --dry-run=client -o yaml | kubectl apply -f -
   ```

---

## Main Tasks

### ▶️ `task run`: Start the operator in development mode

Run the operator locally using:

```bash
task run
```

This will:
- Build and start the operator using `kopf run`
- Load Python modules from the `nuvolaris/` folder
- Watch Kubernetes events and Custom Resources from your current cluster context

📌 Make sure your current Kubernetes context is active before running this.

You can check the state of the CouchDB pod and related resources with:

```bash
kubectl get all -n nuvolaris
```

---

### ✅ `task verify`: Deploy a test CouchDB resource and check behavior

Run:

```bash
task verify
```

This will:
- Apply a test `CouchDB` custom resource defined in `tests/test-couchdb.yaml`
- Trigger the operator to generate:
  - CouchDB Secret
  - StatefulSet
  - Service
  - Init Job (`couchdb-init`)

To manually verify the result:

```bash
kubectl get pods -n nuvolaris
kubectl logs -n openserverless-system -l app=couchdb-operator
```

To check if CouchDB is responding:

```bash
kubectl run curlpod --rm -i -t -n nuvolaris --image=curlimages/curl --restart=Never --   curl -u whisk_admin:some_passw0rd http://couchdb.nuvolaris.svc.cluster.local:5984/_all_dbs
```

---

## Cleanup

To remove the CouchDB resource created by `task verify`:

```bash
kubectl delete -f tests/test-couchdb.yaml
```

---

## Debugging

If running the operator with `task run`, logs will appear directly in your terminal.

If the operator is running as a pod inside the cluster:

```bash
kubectl logs -n openserverless-system -l app=couchdb-operator -f
```

---

## Project Structure

- `nuvolaris/` – Python logic of the operator
- `tests/test-couchdb.yaml` – test resource (CR) to verify functionality
- `nuvolaris/templates/` – Jinja2 templates used to generate Kubernetes manifests

---

## Author

**mobs75** – <https://github.com/mobs75>
