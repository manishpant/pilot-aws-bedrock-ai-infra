resource "aws_resourcegroups_group" "this" {
  name        = var.resource_group_name
  description = "Resources tagged Environment=${var.environment}"

  resource_query {
    query = jsonencode({
      ResourceTypeFilters = ["AWS::AllSupported"]
      TagFilters = [
        {
          Key    = "Environment"
          Values = [var.environment]
        }
      ]
    })
  }

  tags = {
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}
