data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "6.0.1"

  name                = var.name
  cidr = var.vpc_cidr
  azs  = local.azs

  private_subnets = [for index, az in local.azs : cidrsubnet(var.vpc_cidr, 4, index)]
  public_subnets  = [for index, az in local.azs : cidrsubnet(var.vpc_cidr, 4, index + 8)]

  enable_dns_hostnames = true
  enable_nat_gateway   = true
  single_nat_gateway   = true

  public_subnet_tags = {
    "kubernetes.io/role/elb" = "1"
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = "1"
  }
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "21.0.0"

  name                = var.name
  kubernetes_version  = var.kubernetes_version
  authentication_mode = "API"

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  endpoint_private_access      = true
  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.allowed_api_cidrs

  access_entries = {
    admin = {
      principal_arn = var.admin_principal_arn
      policy_associations = {
        admin = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = {
            type = "cluster"
          }
        }
      }
    }
  }

  addons = {
    coredns    = {}
    kube-proxy = {}
    vpc-cni = {
      before_compute = true
    }
  }

  eks_managed_node_groups = { for name, group in {
    cpu = {
      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = [var.cpu_instance_type]
      capacity_type  = "ON_DEMAND"
      min_size       = var.cpu_node_count
      max_size       = var.cpu_node_count
      desired_size   = var.cpu_node_count
      labels = {
        "inference/workload" = "cpu"
      }
    }
    gpu = {
      ami_type       = "AL2023_x86_64_NVIDIA"
      instance_types = [var.gpu_instance_type]
      capacity_type  = "ON_DEMAND"
      min_size       = var.gpu_node_count
      max_size       = var.gpu_node_count
      desired_size   = var.gpu_node_count
      block_device_mappings = {
        root = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = 100
            volume_type           = "gp3"
            encrypted             = true
            delete_on_termination = true
          }
        }
      }
      labels = {
        "inference/workload" = "gpu"
      }
      taints = {
        gpu = {
          key    = "nvidia.com/gpu"
          value  = "true"
          effect = "NO_SCHEDULE"
        }
      }
    }
  } : name => group if name != "gpu" || var.gpu_node_count > 0 }
}
