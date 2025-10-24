# terraform/staging/resource_group.tf


resource "azurerm_resource_group" "existing" {
  name     = "${var.prefix}-rg"
}
