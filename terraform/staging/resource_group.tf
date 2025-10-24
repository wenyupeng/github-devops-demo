# terraform/staging/resource_group.tf


data "azurerm_resource_group" "existing" {
  name     = "${var.prefix}-rg"
}
