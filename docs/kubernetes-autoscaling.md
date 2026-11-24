# CodeDeck Kubernetes Autoscaling Prerequisites

CodeDeck does not install Metrics Server or change cluster-wide components. Configure autoscaling only after an operator has checked the target K3s or Kind cluster.

## Read-only checks

Run these against the intended cluster and namespace:

```sh
kubectl cluster-info
kubectl version
kubectl api-versions | grep '^autoscaling/v2$'
kubectl get apiservices v1beta1.metrics.k8s.io
kubectl top nodes
kubectl top pods -n <namespace>
kubectl get deployment <deployment> -n <namespace> -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{" cpu="}{.resources.requests.cpu}{" memory="}{.resources.requests.memory}{"\n"}{end}'
```

The configured developer Kind endpoint in the implementation environment was `127.0.0.1:64826` and refused connections during read-only inspection. Therefore live Metrics Server, HPA API and workload requests could not be verified for Kind or the remote EC2 K3s cluster. Use the CodeDeck Monitoring page and these checks after the intended cluster is reachable.

## Metrics Server

If `v1beta1.metrics.k8s.io` is absent or `kubectl top` fails, compare the cluster Kubernetes version with the Metrics Server compatibility matrix and review K3s aggregation-layer, kubelet certificate and network requirements. Metrics Server is the upstream resource-metrics provider used by CPU and memory HPA; its documentation explains the required control-plane-to-Metrics-Server and Metrics-Server-to-kubelet paths: [Metrics Server requirements and installation](https://github.com/kubernetes-sigs/metrics-server#requirements).

Only after that review and an operator's change approval, install a **versioned** upstream manifest compatible with the cluster version. For example, Metrics Server `v0.8.1` publishes a version-pinned manifest (verify its compatibility before use):

```sh
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.8.1/components.yaml
```

Wait for the deployment and APIService to become available, then repeat `kubectl top`. Do not add `--kubelet-insecure-tls` to a production cluster to hide certificate problems; resolve kubelet certificate trust and network reachability instead. The compatibility matrix and secure deployment notes are maintained by the [upstream Metrics Server project](https://github.com/kubernetes-sigs/metrics-server).

## Resource requests and replica ownership

CPU utilization targets require CPU requests on every target container. Memory utilization targets additionally require memory requests. CodeDeck checks these conditions before creating an HPA. Generated CodeDeck deployment manifests include CPU and memory requests; repository-provided manifests may not.

While enabled, the HPA controller owns the target Deployment's replica count within the configured minimum and maximum. CodeDeck records the original manual count and restores it when its HPA is removed. Namespace cleanup removes HPA objects with other namespaced application resources; PVCs and their data remain protected by the existing cleanup behavior.

## Controlled demonstration

Use an isolated CodeDeck **staging** deployment and a test-only route that deliberately consumes CPU. Enable autoscaling with a modest range (for example 1–3 replicas), and confirm the panel's initial current/desired replicas and utilization. From a test client, run a short bounded load such as `hey -z 60s -c 2 -q 2 http://<staging-host>/<cpu-test-route>`; keep the duration, concurrency and request rate low, and stop early if the service or cluster becomes unhealthy. Observe actual CPU utilization, the HPA desired count, and ready replicas in CodeDeck, then wait through the configured scale-down stabilization period after the load stops. The default scale-down stabilization is 300 seconds. The panel displays observed Kubernetes samples rather than a simulated sequence. Do not run load generation against production or a shared endpoint. CodeDeck does not start traffic automatically.

The HPA follows the Kubernetes autoscaling/v2 resource API and CPU utilization behavior described in the [Kubernetes HPA documentation](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/).
