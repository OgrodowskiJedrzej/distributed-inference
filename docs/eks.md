# EKS GPU boilerplate

This profile is a development scaffold, not a verified AWS deployment. It keeps
`gateway -> router -> worker` and the `mock-qwen` API model name. Local profiles
are unchanged. Nothing in this repository automatically provisions AWS resources.

## Layout and assumptions

- `infrastructure/`: VPC in two availability zones, one NAT gateway, EKS, IAM
  access entry, fixed-size CPU/GPU managed node groups, and immutable gateway ECR.
- `deploy/kubernetes/overlays/eks-gpu/`: existing base plus one real GPU worker.
- `deploy/platform/redis/redis-values-eks.yaml`: ephemeral development Redis.
- `deploy/platform/nvidia-device-plugin/values-eks.yaml`: GPU device advertisement.

The default instances are x86_64 `m6i.large` and `g5.xlarge`, with one node each.
Check regional availability, GPU quotas, supported Kubernetes versions, pricing,
and AMI compatibility before provisioning. The GPU group uses an EKS AL2023
NVIDIA AMI with drivers/toolkit supplied by AWS. Install the device plugin below,
not another driver stack. Nodes are private; outbound downloads use NAT. The API
endpoint allows public access only from the configured CIDRs and private access
from the VPC. Application Services remain ClusterIP.

The worker pins vLLM `v0.8.5`, SmolLM2-135M-Instruct revision
`12fd25f77366fa6b3b4b768ec3050bf629380bac`, float16, 1024 context tokens, two
sequences, and GPU memory utilization 0.5. This image and GPU configuration need
live validation. They differ from the local CPU backend, float32, and KV budget;
performance and numerical results are not equivalent. This is a smoke-test model,
not a representative GPU benchmark. No experiment scripts or outputs are changed.

Model cache is an emptyDir and is lost on Pod replacement. Redis state is also
nonpersistent. One GPU worker uses Recreate updates to avoid requiring a second
GPU during rollout; updates interrupt inference. No node autoscaler, HPA, shared
KV, persistent storage, ingress, TLS, or monitoring installation is included.

## Local preparation without an AWS account

Install Terraform >= 1.5.7, kubectl, Helm, and Docker. AWS CLI is needed later.
From the repository root:

```sh
terraform -chdir=infrastructure fmt -check
terraform -chdir=infrastructure init -backend=false
terraform -chdir=infrastructure validate
kubectl kustomize deploy/kubernetes/overlays/eks-gpu
```

Initialization downloads modules/providers but does not provision AWS resources.
Keep the generated `.terraform.lock.hcl` when initialization is first performed.
Terraform currently uses local state; keep it private and backed up. A remote
state backend is a separate task before shared/team use.

## Later: provision the cluster

The following commands are for a configured AWS account and incur charges.
Authenticate with an IAM identity, not account root. Copy the example to
`infrastructure/terraform.tfvars` and replace all placeholders, including a
supported EKS version, the persistent IAM role/user ARN for cluster administration
(not an STS assumed-role session ARN), and your public IP CIDR.

```sh
cp infrastructure/terraform.tfvars.example infrastructure/terraform.tfvars
terraform -chdir=infrastructure init
terraform -chdir=infrastructure plan -out=eks.tfplan
terraform -chdir=infrastructure apply eks.tfplan
terraform -chdir=infrastructure output -raw configure_kubectl
```

Run the printed kubeconfig command using the authorized administrator identity.
If that differs from the provisioning identity, select its AWS profile or assume
that role first. Changing desired_size after creation does not itself resize
managed node groups through this module; review min/max settings and the plan.

## Later: build and deploy

Set `AWS_REGION` to the configured region and `GATEWAY_TAG` to a unique build ID.
Build for the x86_64 nodes, including when building on an ARM development machine:

```sh
GATEWAY_REPOSITORY=$(terraform -chdir=infrastructure output -raw gateway_repository_url)
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${GATEWAY_REPOSITORY%%/*}"
docker buildx build --platform linux/amd64 --tag "$GATEWAY_REPOSITORY:$GATEWAY_TAG" --push components/gateway
```

Replace the gateway repository and tag placeholders in the EKS Kustomization.
Never reuse an existing ECR tag. The worker and router use their public images.

Install the NVIDIA device plugin, pinned to chart version 0.17.1:

```sh
helm upgrade --install nvidia-device-plugin nvidia-device-plugin \
  --repo https://nvidia.github.io/k8s-device-plugin \
  --version 0.17.1 --namespace nvidia-device-plugin --create-namespace \
  --values deploy/platform/nvidia-device-plugin/values-eks.yaml --wait
kubectl get nodes -l inference/workload=gpu -o 'custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu'
kubectl apply -f deploy/kubernetes/base/namespace.yaml
```

Create Secret `redis-auth` in namespace `inference`, containing key
`redis-password`, outside version control. Do not regenerate it on each deploy.
Choose and pin a compatible Redis Helm chart version in `REDIS_CHART_VERSION`;
verify its image availability and values before installation:

```sh
helm upgrade --install inference-redis redis \
  --repo https://charts.bitnami.com/bitnami \
  --version "$REDIS_CHART_VERSION" --namespace inference \
  --values deploy/platform/redis/redis-values-eks.yaml --wait
kubectl apply -k deploy/kubernetes/overlays/eks-gpu
kubectl rollout status deployment/mock-vllm -n inference --timeout=20m
kubectl rollout status deployment/vllm-router -n inference
kubectl rollout status deployment/inference-gateway -n inference
kubectl port-forward -n inference service/inference-gateway 18000:8000
```

In a separate terminal:

```sh
curl --fail http://localhost:18000/healthz
curl --fail http://localhost:18000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"mock-qwen","messages":[{"role":"user","content":"Hello"}],"max_tokens":8,"temperature":0}'
```

The existing `tests/e2e/pipeline.sh` targets kind and installs local dependencies;
do not use it as an EKS deployment script.

## Later: remove the development environment

Delete the application and Helm releases while cluster access still works:

```sh
kubectl delete -k deploy/kubernetes/overlays/eks-gpu
helm uninstall inference-redis -n inference
helm uninstall nvidia-device-plugin -n nvidia-device-plugin
terraform -chdir=infrastructure plan -destroy
```

Review the destroy plan before running `terraform -chdir=infrastructure destroy`.
ECR deliberately has no force-delete setting: a nonempty repository blocks its
removal. Explicitly review and remove its images if deleting the repository is
intended. Preserve any future outputs or experiment artifacts before teardown.

References: [EKS module v21](https://github.com/terraform-aws-modules/terraform-aws-eks/tree/v21.0.0),
[VPC module](https://github.com/terraform-aws-modules/terraform-aws-vpc/tree/v6.0.1),
[AWS NVIDIA AMIs](https://docs.aws.amazon.com/eks/latest/userguide/ml-eks-optimized-ami.html),
[NVIDIA plugin](https://github.com/NVIDIA/k8s-device-plugin/tree/v0.17.1).
