variable "region" {
  description = "Region for the resource group."
  type        = string
  default     = "eu-north-1"
}

variable "environment" {
  description = "Environment tag the resource group selects."
  type        = string
  default     = "dev"
}

variable "resource_group_name" {
  description = "Name of the AWS resource group."
  type        = string
  default     = "pilot-infra"
}
