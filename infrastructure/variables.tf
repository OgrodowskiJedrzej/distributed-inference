variable "name" {
  type    = string
  default = "inference-dev"
}

variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "kubernetes_version" {
  type = string
}

variable "admin_principal_arn" {
  type = string
}

variable "allowed_api_cidrs" {
  type = list(string)
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "cpu_instance_type" {
  type    = string
  default = "m6i.large"
}

variable "cpu_node_count" {
  type    = number
  default = 1
}

variable "gpu_instance_type" {
  type    = string
  default = "g5.xlarge"
}

variable "gpu_node_count" {
  type    = number
  default = 1
}

variable "tags" {
  type    = map(string)
  default = {}
}
