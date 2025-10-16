#!/bin/bash
set -e

# Genera un tag unico basato sul timestamp
TAG="test-$(date +%s)"
echo "🏷️  Tag immagine: $TAG"

echo ""
echo "🔧 1. Build immagine Docker..."
docker build --no-cache -t couchdb-operator:$TAG -f Dockerfile .
docker tag couchdb-operator:$TAG couchdb-operator:latest

echo ""
echo "📦 2. Salvataggio immagine in tar..."
docker save couchdb-operator:$TAG couchdb-operator:latest -o /tmp/couchdb-operator.tar

echo ""
echo "📥 3. Import in MicroK8s (richiede sudo)..."
sudo microk8s ctr image import /tmp/couchdb-operator.tar

echo ""
echo "♻️  4. Reload operator (rollout restart)..."
kubectl rollout restart deployment/couchdb-operator -n openserverless-system
kubectl rollout status deployment/couchdb-operator -n openserverless-system --timeout=60s

echo ""
echo "⏳ 5. Attendo che il nuovo pod sia pronto..."
sleep 5
kubectl wait --for=condition=ready pod -l app=couchdb-operator -n openserverless-system --timeout=60s

echo ""
echo "🗑️  6. Cancello l'istanza CouchDB esistente..."
kubectl delete couchdbinstance test-couchdb -n openserverless-system --ignore-not-found

echo ""
echo "⏳ 7. Attendo la cancellazione completa..."
sleep 3

echo ""
echo "✨ 8. Creo nuova istanza CouchDB..."
kubectl apply -f couchdb-instance.yaml -n openserverless-system

echo ""
echo "⏳ 9. Attendo un po' per dare tempo all'operator di processare..."
sleep 5

echo ""
echo "📋 10. Mostro i log dell'operator (ultimi 50 righe)..."
echo "======================================================"
kubectl logs -n openserverless-system -l app=couchdb-operator --tail=50

echo ""
echo ""
echo "📊 11. Verifico lo status della risorsa CouchDB..."
echo "======================================================"
kubectl get couchdbinstance test-couchdb -n openserverless-system -o yaml | grep -A 10 "^status:"

echo ""
echo "✅ Test completato!"
