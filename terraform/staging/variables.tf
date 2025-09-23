# sit722-10-2d/terraform/variables.tf

variable "prefix" {
  description = "Prefix for all resource names"
  type        = string
  default     = "sit722devops102d"
}

variable "location" {
  description = "Azure region"
  type        = string
  default     = "eastus"
}

variable "kubernetes_version" {
  default = "1.31.7"
}
